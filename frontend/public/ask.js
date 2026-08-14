/* ask.js — askText / askConfirm / askPick: the in-page replacements for
 * window.prompt and window.confirm.
 *
 * A native dialog draws through the browser chrome, not the page, so none of
 * the design system reaches it — and the desktop build (bgate_ui/desktop.py,
 * pywebview over Edge WebView2) registers no script-dialog handling at all,
 * where WebView2 commonly suppresses window.prompt outright. Every prompt() in
 * this app was therefore a DEAD BUTTON in the desktop shell, not merely an ugly
 * one. These three are the replacements.
 *
 * THE CONTRACT, carried over from seats/art.js:_ask and non-negotiable:
 *   askText  resolves the STRING on confirm and NULL on cancel — "" means
 *            confirmed-empty, null means the operator backed out. Several call
 *            sites used to collapse the two with `|| ""` and so submitted a
 *            blank-reason rejection on Escape.
 *   askConfirm resolves true/false and never rejects.
 *   askPick  resolves the chosen item's value, or null.
 * All three are promise-returning, so converting a call site is one line plus
 * making the enclosing function async.
 *
 * Styles live in app.css under @layer components (.ask-*), not in an
 * injectStyle() array — this is shell chrome shared by index.html, five
 * modules and the seats, and it sits alongside .toasts / .insp-scrim.
 *
 * Registered as the FIRST module script so window.askText et al exist before
 * anything that calls them parses.
 */
(function () {
  "use strict";

  // Local copy rather than a dependency on index.html's esc(), same as
  // bgselect.js — this file must not care about script order.
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* One question at a time. The shell has no z-order story for two of these,
     and a second overlay would hide the one already being awaited. */
  var LIVE = null;

  /* ---- shared mount ------------------------------------------------------
   * cfg: { host, cancel, wide }.  `host` present renders INLINE inside that
   * element (art.js's shape: previous instance removed, scrolled into view);
   * absent gives a centred card over --scrim.
   * build(card, done) fills the card and returns the element to focus.
   */
  function mount(cfg, build) {
    cfg = cfg || {};
    var cancel = ("cancel" in cfg) ? cfg.cancel : null;

    return new Promise(function (resolve) {
      var settled = false, root = null, trigger = null;
      try { trigger = document.activeElement; } catch (e) {}

      function done(v) {
        if (settled) return;
        settled = true;
        if (LIVE && LIVE.root === root) LIVE = null;
        try { document.removeEventListener("keydown", onEsc, true); } catch (e) {}
        try { if (root && root.parentNode) root.parentNode.removeChild(root); } catch (e) {}
        // Focus goes back to whatever opened this; a keyboard operator must not
        // be dropped at the top of the document.
        try { if (trigger && trigger.isConnected && trigger.focus) trigger.focus(); } catch (e) {}
        resolve(v);
      }

      function onEsc(e) {
        if (e.key !== "Escape") return;
        // A BGSelect popup owns Escape while it is open — closing the whole
        // question because someone dismissed a dropdown would lose their typing.
        if (document.querySelector(".bgs-pop")) return;
        e.preventDefault(); e.stopPropagation();
        done(cancel);
      }

      try {
        if (LIVE) LIVE.close();

        var host = cfg.host && cfg.host.appendChild ? cfg.host : null;
        var card;
        if (host) {
          var old = host.querySelector(".ask-inline");
          if (old) old.remove();
          root = document.createElement("div");
          root.className = "ask ask-inline";
          card = root;
          host.appendChild(root);
        } else {
          root = document.createElement("div");
          root.className = "ask-scrim";
          card = document.createElement("div");
          card.className = "ask " + (cfg.wide ? "ask-pickbox" : "ask-card");
          card.setAttribute("role", "dialog");
          card.setAttribute("aria-modal", "true");
          root.appendChild(card);
          document.body.appendChild(root);
          root.addEventListener("mousedown", function (e) {
            if (e.target === root) done(cancel);
          });
        }

        /* Every keystroke stays inside the card. audiolab, spriteedit and
           sceneview bind single-key tool shortcuts on document, so without this
           typing a reason drives the editor underneath. */
        card.addEventListener("keydown", function (e) {
          e.stopPropagation();
          if (e.key === "Tab" && !host) trapTab(e, card);
        });
        document.addEventListener("keydown", onEsc, true);

        LIVE = { root: root, close: function () { done(cancel); } };

        var focus = build(card, done);
        setTimeout(function () {
          try {
            if (!focus || !focus.focus) return;
            // A <select> BGSelect has already enhanced is off the tab order —
            // the button standing in for it is what takes focus.
            var f = focus._bgsBtn || focus;
            f.focus();
            if (f.setSelectionRange && typeof f.value === "string") {
              f.setSelectionRange(f.value.length, f.value.length);
            }
          } catch (e) {}
        }, 0);
        if (host) { try { root.scrollIntoView({ block: "nearest" }); } catch (e) {} }
      } catch (e) {
        // A DOM failure must never hang a caller that is awaiting this.
        try { console.warn("[ask] " + e); } catch (e2) {}
        done(cancel);
      }
    });
  }

  var FOCUSABLE = 'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), ' +
                  'select:not([disabled]), [tabindex="0"]';

  function isBtn(el) {
    return !!(el && el.closest && el.closest("button"));
  }

  function trapTab(e, card) {
    var els = Array.prototype.filter.call(card.querySelectorAll(FOCUSABLE), function (el) {
      return el.getAttribute("tabindex") !== "-1" && el.offsetParent !== null;
    });
    if (!els.length) return;
    var i = els.indexOf(document.activeElement);
    var next = e.shiftKey ? (i <= 0 ? els.length - 1 : i - 1)
                          : (i === -1 || i === els.length - 1 ? 0 : i + 1);
    e.preventDefault();
    els[next].focus();
  }

  /* A body is a string or an array of paragraphs. sceneview builds the array
     already; index.html concatenates a three-sentence explanation. Neither may
     be shortened — on a destructive confirm the explanation IS the safety. */
  function bodyHTML(body) {
    if (body == null || body === "") return "";
    var parts = Array.isArray(body) ? body : String(body).split(/\n{2,}/);
    return parts.filter(function (p) { return String(p).trim(); }).map(function (p) {
      return '<p class="ask-p">' + esc(p).replace(/\n/g, "<br>") + "</p>";
    }).join("");
  }

  /* ---- askText -----------------------------------------------------------
   * opts: { title, body, label, value, placeholder, ok, cancel, required,
   *         multiline (default true), field:{type,options}, host }
   *   -> Promise<string|null>
   * opts.fields: [ {name,type,label,value,placeholder,options,required,rows} ]
   *   -> Promise<object|null>, keyed by field name. One form instead of a run
   *      of sequential questions (world.js's addEntity asks three).
   */
  function normField(f, name, defType) {
    f = f || {};
    return {
      name: name,
      type: f.type || defType,
      label: f.label || "",
      value: f.value == null ? "" : String(f.value),
      placeholder: f.placeholder || "",
      options: f.options || [],
      required: !!f.required,
      rows: f.rows || 3,
    };
  }

  function fieldHTML(d) {
    var id = "ask-f-" + d.name;
    var body;
    if (d.type === "select") {
      body = '<select data-f="' + esc(d.name) + '" id="' + esc(id) + '">' +
        d.options.map(function (o) {
          var v = (o && typeof o === "object") ? o.value : o;
          var l = (o && typeof o === "object") ? (o.label == null ? o.value : o.label) : o;
          return '<option value="' + esc(v) + '"' + (String(v) === d.value ? " selected" : "") +
                 ">" + esc(l) + "</option>";
        }).join("") + "</select>";
    } else if (d.type === "textarea") {
      body = '<textarea data-f="' + esc(d.name) + '" id="' + esc(id) + '" rows="' + d.rows +
             '" placeholder="' + esc(d.placeholder) + '">' + esc(d.value) + "</textarea>";
    } else {
      body = '<input type="text" data-f="' + esc(d.name) + '" id="' + esc(id) +
             '" value="' + esc(d.value) + '" placeholder="' + esc(d.placeholder) +
             '" autocomplete="off">';
    }
    return '<label class="ask-field">' +
      (d.label ? '<span class="ask-label">' + esc(d.label) +
                 (d.required ? "" : ' <span class="ask-opt">optional</span>') + "</span>" : "") +
      body + '<span class="ask-err" hidden></span></label>';
  }

  function askText(opts) {
    opts = opts || {};
    var multi = Array.isArray(opts.fields) && opts.fields.length > 0;
    var descs = multi
      ? opts.fields.map(function (f, i) { return normField(f, f.name || ("f" + i), "text"); })
      : [normField({
          type: (opts.field && opts.field.type) || (opts.multiline === false ? "text" : "textarea"),
          options: opts.field && opts.field.options,
          label: opts.label, value: opts.value, placeholder: opts.placeholder,
          required: opts.required, rows: opts.rows,
        }, "value", "textarea")];

    var hasTA = descs.some(function (d) { return d.type === "textarea"; });

    return mount({ host: opts.host, cancel: null }, function (card, done) {
      card.innerHTML =
        (opts.title ? '<div class="ask-title">' + esc(opts.title) + "</div>" : "") +
        bodyHTML(opts.body) +
        '<div class="ask-fields">' + descs.map(fieldHTML).join("") + "</div>" +
        '<div class="ask-btns">' +
          '<button type="button" class="ask-btn ask-primary" data-a="ok">' +
            esc(opts.ok || "confirm") + "</button>" +
          '<button type="button" class="ask-btn" data-a="no">' +
            esc(opts.cancel || "cancel") + "</button>" +
          '<span class="ask-hint">' +
            (hasTA ? "Ctrl+Enter confirms · Esc cancels" : "Enter confirms · Esc cancels") +
          "</span>" +
        "</div>";

      function elFor(d) { return card.querySelector('[data-f="' + d.name + '"]'); }

      function submit() {
        var out = {}, bad = null;
        descs.forEach(function (d) {
          var el = elFor(d), v = el ? String(el.value) : "";
          out[d.name] = v;
          if (d.required && !v.trim() && !bad) bad = { d: d, el: el };
        });
        if (bad) {
          var err = bad.el && bad.el.parentNode.querySelector(".ask-err");
          if (err) { err.textContent = "required"; err.hidden = false; }
          if (bad.el) bad.el.focus();
          return;
        }
        done(multi ? out : out[descs[0].name]);
      }

      card.querySelector('[data-a="ok"]').onclick = submit;
      card.querySelector('[data-a="no"]').onclick = function () { done(null); };

      descs.forEach(function (d) {
        var el = elFor(d);
        if (!el) return;
        el.addEventListener("input", function () {
          var err = el.parentNode.querySelector(".ask-err");
          if (err) err.hidden = true;
        });
        el.addEventListener("keydown", function (e) {
          if (e.key !== "Enter") return;
          // A textarea keeps Enter for newlines; everything else confirms on it.
          if (d.type === "textarea" && !(e.ctrlKey || e.metaKey)) return;
          e.preventDefault();
          submit();
        });
      });

      return elFor(descs[0]);
    });
  }

  /* ---- askConfirm --------------------------------------------------------
   * opts: { title, body, ok, cancel, danger, host } -> Promise<boolean>
   * danger:true dresses the confirm in --bad AND drops the Enter binding — the
   * keystroke that dismissed the last dialog must not also approve a write that
   * has no undo endpoint behind it.
   */
  function askConfirm(opts) {
    opts = opts || {};
    var danger = !!opts.danger;
    return mount({ host: opts.host, cancel: false }, function (card, done) {
      if (danger) card.classList.add("ask-danger");
      card.innerHTML =
        (opts.title ? '<div class="ask-title">' + esc(opts.title) + "</div>" : "") +
        bodyHTML(opts.body) +
        '<div class="ask-btns">' +
          '<button type="button" class="ask-btn ' + (danger ? "ask-bad" : "ask-primary") +
            '" data-a="ok">' + esc(opts.ok || "confirm") + "</button>" +
          '<button type="button" class="ask-btn" data-a="no">' +
            esc(opts.cancel || "cancel") + "</button>" +
          '<span class="ask-hint">' +
            (danger ? "Esc cancels" : "Enter confirms · Esc cancels") + "</span>" +
        "</div>";
      card.querySelector('[data-a="ok"]').onclick = function () { done(true); };
      card.querySelector('[data-a="no"]').onclick = function () { done(false); };
      if (!danger) {
        card.addEventListener("keydown", function (e) {
          // Enter on a focused button is that button's own click, not a confirm
          // — otherwise Enter on "cancel" would confirm.
          if (e.key !== "Enter" || isBtn(e.target)) return;
          e.preventDefault(); done(true);
        });
      }
      // A destructive question opens with cancel under the cursor.
      return card.querySelector(danger ? '[data-a="no"]' : '[data-a="ok"]');
    });
  }

  /* ---- askPick -----------------------------------------------------------
   * opts: { title, items:[{value,label,meta,tags:[{text,tone}]}],
   *         fetch: async q => ({items,total,truncated}), value, placeholder,
   *         empty, actions:[{label,onClick(done)}] } -> Promise<value|null>
   * The promise-returning generalisation of AudioLab.pick(): same anatomy, plus
   * the roving arrow-key focus and Enter-to-take that pick() never had.
   * AudioLab.pick() / SpriteEdit.pick() stay where they are — they are onclick
   * entry points that call open() rather than returning a value.
   */
  function askPick(opts) {
    opts = opts || {};
    return mount({ cancel: null, wide: true }, function (card, done) {
      card.innerHTML =
        '<div class="ask-pickbar">' +
          '<span class="ask-title">' + esc(opts.title || "pick one") + "</span>" +
          '<input class="ask-filter" type="search" spellcheck="false" autocomplete="off" ' +
            'aria-label="Filter" placeholder="' + esc(opts.placeholder || "filter…") + '">' +
          '<span class="ask-count"></span>' +
          '<span class="ask-spacer"></span>' +
          '<span class="ask-pickacts"></span>' +
          '<button type="button" class="ask-btn" data-a="no">close</button>' +
        "</div>" +
        '<div class="ask-list" role="listbox" tabindex="-1">' +
          '<div class="ask-empty">scanning…</div></div>';

      var list = card.querySelector(".ask-list");
      var countEl = card.querySelector(".ask-count");
      var filter = card.querySelector(".ask-filter");
      var shown = [];

      card.querySelector('[data-a="no"]').onclick = function () { done(null); };

      var acts = card.querySelector(".ask-pickacts");
      (opts.actions || []).forEach(function (a) {
        var b = document.createElement("button");
        b.type = "button"; b.className = "ask-btn"; b.textContent = a.label || "action";
        b.onclick = function () { try { a.onClick(done); } catch (e) { done(null); } };
        acts.appendChild(b);
      });

      function mark(row) {
        Array.prototype.forEach.call(list.querySelectorAll(".ask-row.on"), function (r) {
          r.classList.remove("on"); r.setAttribute("aria-selected", "false");
        });
        if (!row) return;
        row.classList.add("on");
        row.setAttribute("aria-selected", "true");
        try { row.scrollIntoView({ block: "nearest" }); } catch (e) {}
      }

      function paint(items, total, truncated) {
        shown = items || [];
        countEl.textContent = (truncated || (total != null && total > shown.length))
          ? shown.length + " of " + total
          : shown.length + (shown.length === 1 ? " item" : " items");
        list.innerHTML = shown.length ? shown.map(function (it, i) {
          return '<div class="ask-row" role="option" aria-selected="false" data-i="' + i + '">' +
            '<span class="ask-row-label">' + esc(it.label == null ? it.value : it.label) + "</span>" +
            (it.tags || []).map(function (t) {
              return '<span class="ask-tag' + (t.tone ? " t-" + esc(t.tone) : "") + '">' +
                     esc(t.text) + "</span>";
            }).join("") +
            (it.meta ? '<span class="ask-row-meta">' + esc(it.meta) + "</span>" : "") +
            "</div>";
        }).join("") : '<div class="ask-empty">' + esc(opts.empty || "nothing matches") + "</div>";

        var pre = opts.value == null ? null : shown.findIndex(function (it) {
          return String(it.value) === String(opts.value);
        });
        mark(list.querySelector('.ask-row[data-i="' + (pre != null && pre >= 0 ? pre : 0) + '"]'));
      }

      function localFilter(q) {
        var items = opts.items || [];
        if (!q) return items;
        var n = q.toLowerCase();
        return items.filter(function (it) {
          return ((it.label || "") + " " + (it.meta || "") + " " + (it.value || ""))
            .toLowerCase().indexOf(n) >= 0;
        });
      }

      var seq = 0;
      function load(q) {
        if (!opts.fetch) { var l = localFilter(q); paint(l, (opts.items || []).length, false); return; }
        var mine = ++seq;
        Promise.resolve(opts.fetch(q)).then(function (d) {
          // A slower earlier query must not overwrite a later one's results.
          if (mine !== seq || !list.isConnected) return;
          d = d || {};
          paint(d.items || [], d.total == null ? (d.items || []).length : d.total, !!d.truncated);
        }).catch(function () {
          if (mine !== seq || !list.isConnected) return;
          paint([], 0, false);
        });
      }

      var timer = null;
      filter.addEventListener("input", function () {
        clearTimeout(timer);
        var v = filter.value.trim();
        timer = setTimeout(function () { load(v); }, 200);
      });

      list.addEventListener("click", function (e) {
        var row = e.target.closest(".ask-row");
        if (row) done(shown[Number(row.dataset.i)].value);
      });

      card.addEventListener("keydown", function (e) {
        var rows = Array.prototype.slice.call(list.querySelectorAll(".ask-row"));
        if (!rows.length) return;
        var at = rows.indexOf(list.querySelector(".ask-row.on"));
        if (e.key === "ArrowDown") { e.preventDefault(); mark(rows[Math.min(rows.length - 1, at + 1)]); }
        else if (e.key === "ArrowUp") { e.preventDefault(); mark(rows[Math.max(0, at - 1)]); }
        else if (e.key === "Home") { e.preventDefault(); mark(rows[0]); }
        else if (e.key === "End") { e.preventDefault(); mark(rows[rows.length - 1]); }
        else if (e.key === "Enter" && !isBtn(e.target)) {
          e.preventDefault();
          var on = list.querySelector(".ask-row.on");
          if (on) done(shown[Number(on.dataset.i)].value);
        }
      });

      load("");
      return filter;
    });
  }

  window.askText = askText;
  window.askConfirm = askConfirm;
  window.askPick = askPick;
})();
