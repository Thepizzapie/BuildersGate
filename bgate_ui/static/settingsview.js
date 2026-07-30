/* settingsview.js — every switch in one place, rendered FROM the server's
 * description of itself.
 *
 * Four features each put their switch in a different mechanism: a column on
 * spend_budget, a workspace doc, an env var read inline, a module constant.
 * bgate_core.settings now describes all of them in one registry and
 * GET /api/settings hands that description over verbatim. THIS FILE RENDERS THE
 * DESCRIPTION AND NOTHING ELSE — no key is named here, no range is repeated, no
 * group is hardcoded. Adding a switch is one registry entry in Python and no
 * change in this file, which is the whole point of the exercise; the moment this
 * module starts special-casing a key, the pile it deleted is back.
 *
 * A FIELD THE ENVIRONMENT OWNS IS DISABLED AND SAYS WHOSE IT IS. `source: "env"`
 * (and its `locked` flag) means a variable is supplying or coercing the value, so
 * the control is dead and the row names the variable. A panel that offers to edit
 * a value BGATE_QA_GATE has already forced is the most expensive lie a settings
 * surface can tell — somebody debugs the gate for an hour before finding it in a
 * shell profile. The console's inline gate control already greys itself out for
 * exactly this reason; this generalises it to every row.
 *
 * WRITE, THEN RE-RENDER FROM THE RESPONSE. PATCH returns the whole description
 * again, and the value that comes back is the EFFECTIVE one, which is not always
 * what was sent — an env var can win, a number is clamped by its declared range,
 * a list is deduped. Painting the value we asked for would show a save that is
 * not in force. So the response is the only thing that repaints the row, and a
 * refusal repaints from a fresh GET rather than leaving the control showing
 * something no store agreed to.
 */
(function () {
  "use strict";

  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const trunc = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };

  // A settings page is not a live view: it is read when you open it and after a
  // write. This is the floor under a driver that calls render() on the console's
  // 3s tick — a switch nobody is touching does not need re-fetching that often.
  const STALE_MS = 20000;

  const SOURCE_NOTE = {
    default: "the built-in default",
    stored: "saved for this project",
    env: "forced by the environment",
  };

  const SettingsView = {
    root: null, mounted: false,
    payload: null,            // the last /api/settings body, verbatim
    fields: {},               // key -> field, an index over payload.groups
    _busy: "", _err: null, _lastRead: 0, _reading: false, _sig: "",

    /* ---- mount ----------------------------------------------------------
     * Returns false when the host is not in the DOM yet so a caller can retry,
     * the same contract AgentsConsole.mount() has. The id fallbacks are there
     * because index.html's activateWorkspace() calls activate() with no
     * arguments — the view element is the container in that path. */
    mount(container) {
      if (this.mounted) return true;
      const host = typeof container === "string"
        ? document.getElementById(container)
        : (container || document.getElementById("st-host")
          || document.getElementById("view-settings"));
      if (!host) return false;
      this.root = host;
      this.mounted = true;
      host.classList.add("st-wrap");
      // No <style> block here: every st-* rule lives in app.css. A second copy
      // inside the module parses later and wins, so an app.css fix would have
      // been invisible and nothing would have pointed at the reason.
      host.innerHTML = `<div class="st-shell" id="st-shell">
             <div class="st-empty">loading settings…</div>
           </div>`;

      /* THREE delegated listeners for the whole panel, on the container. Every
         control is rebuilt from the response after every write, so per-element
         handlers would need rebinding on each one and a missed rebind is a
         control that silently stops saving. */
      host.addEventListener("click", e => {
        try { this._onClick(e); } catch (err) { this._warn(err); }
      });
      host.addEventListener("change", e => {
        try { this._onChange(e); } catch (err) { this._warn(err); }
      });
      host.addEventListener("keydown", e => {
        // Enter commits a number/text field. Without it the value only lands on
        // blur, and "I typed it and pressed enter" is the most common way to
        // believe a setting was saved when it was not.
        if (e.key !== "Enter") return;
        const input = e.target && e.target.closest && e.target.closest("[data-st-key]");
        if (!input || input.tagName !== "INPUT") return;
        e.preventDefault();
        input.blur();
      });

      this.refresh(true);
      return true;
    },

    /* index.html hands a view to its module with activate(); the shell calls it
       on every click of the rail item, so it must be cheap and idempotent. */
    activate(container) {
      if (!this.mount(container)) return false;
      this.refresh(false);
      return true;
    },

    /* The driver may hand us either a settings description (straight from a
       PATCH or GET elsewhere) or the console state it happens to be holding.
       Only the first is data; the second is a heartbeat. */
    render(state) {
      if (!this.mounted) return false;
      if (state && !state.__error && Array.isArray(state.groups)) {
        this._absorb(state);
        this.paint();
        return true;
      }
      this.refresh(false);
      return true;
    },

    /* ---- reading -------------------------------------------------------- */
    async refresh(force) {
      if (!this.mounted || this._reading) return;
      if (!force && Date.now() - this._lastRead < STALE_MS) return;
      // A refresh in the middle of typing would take the half-entered number
      // away. Forced reads (mount, an explicit reload, a failed write) still go
      // through: after a refusal the field on screen is the wrong value.
      if (!force && this._typing()) return;
      this._reading = true;
      try {
        const d = await window.readJSON("/api/settings", { groups: [] });
        if (d && d.__error) {
          this._err = { message: String(d.__error) };
          this.paint();
          return;
        }
        // A read that worked clears a read failure but NOT a field refusal: the
        // sentence explaining why a value was rejected is the only record of it,
        // and the re-read that follows a refusal would erase it.
        if (this._err && !this._err.key) this._err = null;
        this._absorb(d);
        this.paint();
      } catch (e) {
        this._warn(e);
      } finally {
        this._reading = false;
        this._lastRead = Date.now();
      }
    },

    _absorb(d) {
      this.payload = d && Array.isArray(d.groups) ? d : { groups: [] };
      const index = {};
      (this.payload.groups || []).forEach(g =>
        (g.fields || []).forEach(f => { if (f && f.key) index[f.key] = f; }));
      this.fields = index;
    },

    field(key) { return this.fields[String(key)] || null; },

    _typing() {
      const a = document.activeElement;
      return !!(a && this.root && this.root.contains(a)
        && (a.tagName === "INPUT" || a.tagName === "TEXTAREA"));
    },

    /* ---- writing --------------------------------------------------------
     * One key per PATCH. The endpoint accepts a whole payload and validates all
     * of it before the first write lands, but one key at a time is what makes a
     * refusal legible: the error sentence names its own key and range, so it can
     * be shown against the control that caused it without unpicking
     * detail.errors — which window.mutate does not carry through anyway. */
    async patch(key, value, el) {
      const f = this.field(key);
      if (!f) return;
      if (f.locked) {
        // Defence in depth: the control is already disabled. This catches the
        // enum/chip/toggle path where a click can still reach a styled div.
        window.toast(`${key} is forced by ${this._vars(f)} — the environment wins`);
        return;
      }
      // A switch that WIDENS a guard asks first. `dispatch.allow_dirty` used to
      // need an environment variable; describing it in the registry made it one
      // click, and one click that lets agents write on top of your uncommitted
      // work is one to make on purpose. Only on the way ON — turning a guard
      // back on is never the dangerous direction.
      if (f.guard && value && !f.value) {
        const yes = await window.askConfirm({
          title: `Turn off a guard?`,
          body: `${key}\n\n${f.help}\n\nThis is recorded in the timeline and in `
              + `the notification drawer either way.`,
          ok: "turn it off", cancel: "leave it on", danger: true,
        });
        if (!yes) return;
      }
      if (this._busy) return;   // the response is the new truth; one at a time
      this._busy = key;
      const button = (el && el.tagName === "BUTTON") ? el : null;
      const r = await window.mutate("/api/settings",
        { method: "PATCH", body: { [key]: value }, button, quiet: true });
      this._busy = "";
      if (!r.ok) {
        // The message already names the key and the legal range (the registry
        // writes it). Shown in the row AND as a toast: the row may be scrolled
        // out of view by the time the click lands.
        this._err = { key, message: r.error, status: r.status };
        window.toast(r.error);
        // 409 means another tab wrote first, 503 that a store refused. Either
        // way the value on screen is not what is stored, so re-read.
        await this.refresh(true);
        return;
      }
      this._err = null;
      this._absorb(r.data);
      this.paint();
      const now = this.field(key);
      const shown = now ? this.show(now, now.value) : String(value);
      // An env var can make the stored value not the effective one. Saying
      // "saved" and showing something else is the lie this panel exists to stop.
      if (now && now.source === "env") {
        window.toast(`${key} saved, but ${this._vars(now)} is overriding it — `
          + `in force: ${shown}`);
      } else {
        window.toast(`${key} → ${shown}`, "ok");
      }
    },

    _vars(f) {
      const vars = Array.isArray(f.env_vars) ? f.env_vars : [];
      return f.env || vars.join(" / ") || "the environment";
    },

    /* ---- events --------------------------------------------------------- */
    _onClick(e) {
      const t = e.target;
      if (!t || !t.closest) return;
      // An explicit re-read is a clean slate — including the last refusal.
      if (t.closest("[data-st-reload]")) { this._err = null; this.refresh(true); return; }
      const hit = t.closest("[data-st-act]");
      if (!hit || hit.disabled) return;
      const key = hit.dataset.stKey;
      const f = this.field(key);
      if (!f) return;
      const act = hit.dataset.stAct;
      if (act === "bool") { this.patch(key, !f.value, hit); return; }
      if (act === "enum") {
        if (String(f.value) === hit.dataset.stVal) return;   // no-op write
        this.patch(key, hit.dataset.stVal, hit);
        return;
      }
      if (act === "chip") {
        const have = Array.isArray(f.value) ? f.value.map(String) : [];
        const one = hit.dataset.stVal;
        const next = have.indexOf(one) >= 0
          ? have.filter(v => v !== one)
          : have.concat([one]);
        this.patch(key, next, hit);
        return;
      }
      if (act === "reset") { this.patch(key, f.default, hit); return; }
    },

    _onChange(e) {
      const input = e.target && e.target.closest && e.target.closest("[data-st-key]");
      if (!input || !input.dataset.stAct) return;
      const key = input.dataset.stKey;
      const f = this.field(key);
      if (!f) return;
      const act = input.dataset.stAct;
      if (act === "num") {
        const raw = String(input.value || "").trim();
        if (raw === "") { this.paint(true); return; }   // blank is not a number
        if (Number(raw) === Number(f.value)) return;
        this.patch(key, raw, input);
        return;
      }
      if (act === "text") {
        const raw = String(input.value || "").trim();
        if (raw === String(f.value == null ? "" : f.value)) return;
        this.patch(key, raw, input);
        return;
      }
      if (act === "csv") {
        const parts = String(input.value || "").split(",")
          .map(s => s.trim()).filter(Boolean);
        this.patch(key, parts, input);
      }
    },

    /* ---- painting ------------------------------------------------------- */
    paint(force) {
      if (!this.mounted) return;
      const shell = document.getElementById("st-shell");
      if (!shell) return;
      try {
        const sig = this._signature();
        if (!force && sig === this._sig) return;
        this._sig = sig;
        if (!this.payload) {
          shell.innerHTML = `<div class="st-empty">loading settings…</div>`;
          return;
        }
        const groups = this.payload.groups || [];
        if (!groups.length) {
          shell.innerHTML = this._head()
            + `<div class="st-empty">the settings registry answered with nothing —
               this build's dashboard is older than its core, or the project could
               not be read</div>`;
          return;
        }
        shell.innerHTML = this._head() + groups.map(g => this._group(g)).join("");
      } catch (e) {
        this._warn(e);
      }
    },

    /* Every value, source and lock in one string. The source belongs in it as
       much as the value does: setting an env var and reloading changes nothing
       else on the page, and a signature that watched only values would leave the
       row claiming to be editable. */
    _signature() {
      const bits = [this._err ? `${this._err.key || ""}:${this._err.message}` : ""];
      (this.payload && this.payload.groups || []).forEach(g =>
        (g.fields || []).forEach(f => bits.push(
          `${f.key}=${JSON.stringify(f.value)}/${f.source}/${f.locked ? 1 : 0}`)));
      return bits.join("|");
    },

    _head() {
      const p = this.payload || {};
      const locked = Object.keys(this.fields)
        .filter(k => this.fields[k].locked).length;
      return `<div class="st-head">
        <div class="st-h1">Settings</div>
        <div class="st-sub">${esc(p.precedence || "env > project stored > default")}
          — each row says which layer won${locked
            ? ` · <span class="st-warnline">${locked} field${locked === 1 ? " is" : "s are"}
                forced by the environment and cannot be edited here</span>` : ""}</div>
        <button class="st-btn" type="button" data-st-reload="1">re-read</button>
      </div>`
        + (this._err && !this._err.key
          ? `<div class="st-err">settings could not be read — ${esc(this._err.message)}</div>`
          : "");
    },

    _group(g) {
      const fields = (g.fields || []);
      if (!fields.length) return "";
      return `<section class="st-group">
        <h3 class="st-gh">${esc(g.name || "Other")}
          <span class="st-gn">${fields.length}</span></h3>
        <div class="st-rows">${fields.map(f => this._row(f)).join("")}</div>
      </section>`;
    },

    _row(f) {
      const bad = this._err && this._err.key === f.key;
      const range = (f.min != null || f.max != null)
        ? `${f.min != null ? f.min : "−∞"}…${f.max != null ? f.max : "∞"}` : "";
      const changed = f.source === "stored"
        && JSON.stringify(f.value) !== JSON.stringify(f.default);
      return `<div class="st-row${f.locked ? " locked" : ""}${bad ? " bad" : ""}"
                   data-st-row="${esc(f.key)}">
        <div class="st-label">
          <code class="st-key">${esc(f.key)}</code>
          ${f.scope === "machine"
            ? `<span class="st-tag" title="Describes this machine or checkout, not the game — it does not travel with the project">machine</span>`
            : ""}
          ${f.locked
            ? `<span class="st-tag env" title="${esc(f.env_override || "")}">${esc(this._vars(f))}</span>`
            : ""}
          <span class="st-src ${esc(f.source)}"
                title="${esc(SOURCE_NOTE[f.source] || f.source)}">${esc(f.source)}</span>
        </div>
        <div class="st-ctl">${this._control(f)}</div>
        <div class="st-help">${esc(f.help || "")}</div>
        <div class="st-foot">
          <span class="st-def">default ${esc(trunc(this.show(f, f.default), 60))}${
            range ? ` · ${esc(range)}` : ""}</span>
          ${changed
            ? `<button class="st-link" type="button" data-st-act="reset"
                       data-st-key="${esc(f.key)}">reset</button>` : ""}
          ${f.locked && f.env_override
            ? `<span class="st-envnote">${esc(f.env_override)}</span>` : ""}
          ${bad ? `<span class="st-badnote">${esc(this._err.message)}</span>` : ""}
        </div>
      </div>`;
    },

    /* One control per declared kind. The kind comes from the registry, so a new
       field of an existing kind renders with no edit here; an UNKNOWN kind falls
       back to a read-only value rather than an input that would POST the wrong
       shape — a control that cannot be right should not pretend. */
    _control(f) {
      const key = esc(f.key);
      const off = f.locked ? " disabled" : "";
      const kind = String(f.kind || "");
      if (kind === "bool") {
        const on = !!f.value;
        return `<button class="st-toggle${on ? " on" : ""}" type="button"
                  data-st-act="bool" data-st-key="${key}"
                  aria-pressed="${on ? "true" : "false"}"${off}>
                  <span class="st-knob"></span></button>
                <span class="st-val">${on ? "on" : "off"}</span>`;
      }
      if (kind === "enum") {
        const choices = Array.isArray(f.choices) ? f.choices : [];
        return `<div class="st-seg" role="group">${choices.map(c => {
          const on = String(f.value) === String(c);
          return `<button class="st-segb${on ? " on" : ""}" type="button"
                    data-st-act="enum" data-st-key="${key}" data-st-val="${esc(c)}"
                    aria-pressed="${on ? "true" : "false"}"${off}>${esc(c)}</button>`;
        }).join("")}</div>`;
      }
      if (kind === "int" || kind === "float") {
        const step = kind === "int" ? "1" : "any";
        return `<input class="st-in st-num" type="number" inputmode="decimal"
                  data-st-act="num" data-st-key="${key}" step="${step}"
                  ${f.min != null ? `min="${esc(f.min)}"` : ""}
                  ${f.max != null ? `max="${esc(f.max)}"` : ""}
                  value="${esc(f.value)}"${off}>`;
      }
      if (kind === "list") {
        const have = Array.isArray(f.value) ? f.value.map(String) : [];
        const choices = Array.isArray(f.choices) ? f.choices : [];
        // A declared choice list is a chip per choice; an open list is text,
        // because there is nothing to enumerate and a chip you cannot add is a
        // dead end.
        if (choices.length) {
          return `<div class="st-chips">${choices.map(c => {
            const on = have.indexOf(String(c)) >= 0;
            return `<button class="st-chip${on ? " on" : ""}" type="button"
                      data-st-act="chip" data-st-key="${key}" data-st-val="${esc(c)}"
                      aria-pressed="${on ? "true" : "false"}"${off}>${esc(c)}</button>`;
          }).join("")}</div>
          <span class="st-val">${have.length} of ${choices.length}</span>`;
        }
        return `<input class="st-in" type="text" data-st-act="csv"
                  data-st-key="${key}" value="${esc(have.join(", "))}"
                  placeholder="comma separated"${off}>`;
      }
      if (kind === "string") {
        return `<input class="st-in" type="text" data-st-act="text"
                  data-st-key="${key}" value="${esc(f.value == null ? "" : f.value)}"
                  placeholder="${esc(f.default ? String(f.default) : "empty")}"${off}>`;
      }
      return `<span class="st-val ro">${esc(trunc(this.show(f, f.value), 80))}
                <i>(kind "${esc(kind)}" — this dashboard has no control for it)</i></span>`;
    },

    /* A value as a sentence fragment. Used for the default line and the toast,
       so it has to be defined for every kind including the empty string — an
       empty default that renders as nothing reads as a bug in the panel. */
    show(f, value) {
      const kind = String((f && f.kind) || "");
      if (kind === "bool") return value ? "on" : "off";
      if (kind === "list") {
        const items = Array.isArray(value) ? value : String(value || "").split(",");
        const clean = items.map(s => String(s).trim()).filter(Boolean);
        return clean.length ? clean.join(", ") : "nothing";
      }
      if (value === "" || value == null) return "empty";
      return String(value);
    },

    _warn(e) { try { console.warn("[settings]", e); } catch (_) { } },

  };

  window.SettingsView = SettingsView;
})();
